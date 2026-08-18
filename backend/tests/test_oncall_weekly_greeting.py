"""Weekly on-call Feishu greeting (`app.services.oncall_weekly_greeting`)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.services import oncall_weekly_greeting

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------
class _FakeFeishuSettings:
    def __init__(self, oncall_greeting_chat_id: str):
        self.oncall_greeting_chat_id = oncall_greeting_chat_id


class _FakeSettings:
    """Stand-in for `Settings` — avoids the real `get_settings()`/lru_cache/
    yaml/env machinery entirely so these tests fully control chat_id,
    feedback_recipient, and frontend_base_url."""

    def __init__(self, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai", frontend_base_url=""):
        self.feishu = _FakeFeishuSettings(chat_id)
        self.feedback_recipient = feedback_recipient
        self.frontend_base_url = frontend_base_url


def _patch_settings(monkeypatch, **kwargs) -> _FakeSettings:
    settings = _FakeSettings(**kwargs)
    monkeypatch.setattr(oncall_weekly_greeting, "get_settings", lambda: settings)
    return settings


async def _seed_single_group(members):
    """Seed exactly one oncall group with a start_date safely in the past.

    With a single group, `resolve_week_group`'s `idx = week_num % n` is
    always 0 no matter what the real wall-clock date is when the test runs
    (`get_current_oncall_info()` computes `week_num` from real `date.today()`,
    not from the `today=` param passed to `send_weekly_greeting` — that param
    only drives `_week_range`/rendering/the idempotency marker). This makes
    "current oncall members" deterministic regardless of when the suite runs.
    """
    from app.db import database as db
    await db.save_oncall_groups([members], created_by="test")
    await db.set_oncall_config("start_date", "2020-01-06")  # a Monday, well in the past


async def _fake_id_map_identity(emails):
    return {e: f"ou_{e.split('@')[0]}" for e in emails}


# ---------------------------------------------------------------------------
# 1-4: _seconds_until_next_monday_9am
# ---------------------------------------------------------------------------
def test_01_seconds_until_next_monday_9am_same_day_before_9():
    # 2026-08-17 is a Monday.
    now = datetime(2026, 8, 17, 6, 0, tzinfo=SHANGHAI_TZ)
    assert oncall_weekly_greeting._seconds_until_next_monday_9am(now) == 3 * 3600


def test_02_seconds_until_next_monday_9am_exact_boundary_rolls_to_next_week():
    """The `now_local >= target` boundary at exactly 09:00:00 on Monday must
    roll forward a full week, not return 0 — this is the easiest edge to get
    backwards (using `>` instead of `>=`)."""
    now = datetime(2026, 8, 17, 9, 0, 0, tzinfo=SHANGHAI_TZ)
    expected = (datetime(2026, 8, 24, 9, 0, tzinfo=SHANGHAI_TZ) - now).total_seconds()
    assert oncall_weekly_greeting._seconds_until_next_monday_9am(now) == expected
    assert expected == 7 * 24 * 3600


def test_03_seconds_until_next_monday_9am_midweek():
    # 2026-08-19 is a Wednesday.
    now = datetime(2026, 8, 19, 12, 0, tzinfo=SHANGHAI_TZ)
    expected = (datetime(2026, 8, 24, 9, 0, tzinfo=SHANGHAI_TZ) - now).total_seconds()
    assert oncall_weekly_greeting._seconds_until_next_monday_9am(now) == expected


def test_04_seconds_until_next_monday_9am_sunday_late_night():
    # 2026-08-23 is a Sunday; days_ahead = (0 - 6) % 7 = 1 -> next day.
    now = datetime(2026, 8, 23, 23, 59, tzinfo=SHANGHAI_TZ)
    expected = (datetime(2026, 8, 24, 9, 0, tzinfo=SHANGHAI_TZ) - now).total_seconds()
    assert oncall_weekly_greeting._seconds_until_next_monday_9am(now) == expected


# ---------------------------------------------------------------------------
# 5: _week_range
# ---------------------------------------------------------------------------
def test_05_week_range_same_pair_for_every_weekday_in_the_week():
    expected = (date(2026, 8, 17), date(2026, 8, 23))  # Monday .. Sunday
    assert oncall_weekly_greeting._week_range(date(2026, 8, 17)) == expected  # Monday
    assert oncall_weekly_greeting._week_range(date(2026, 8, 19)) == expected  # Wednesday
    assert oncall_weekly_greeting._week_range(date(2026, 8, 23)) == expected  # Sunday (weekday()==6)


# ---------------------------------------------------------------------------
# 6-9: _render_greeting
# ---------------------------------------------------------------------------
def test_06_render_all_resolved_order_preserved_no_warning():
    members = ["alice@plaud.ai", "bob@plaud.ai"]
    id_map = {"alice@plaud.ai": "ou_alice", "bob@plaud.ai": "ou_bob"}
    text, unresolved = oncall_weekly_greeting._render_greeting(
        members, id_map, date(2026, 8, 17), date(2026, 8, 23), "https://appllo.example.com",
    )

    assert unresolved == []
    alice_tag = '<at user_id="ou_alice">alice</at>'
    bob_tag = '<at user_id="ou_bob">bob</at>'
    assert alice_tag in text and bob_tag in text
    # order must match `members`, not dict iteration order.
    assert text.index(alice_tag) < text.index(bob_tag)
    assert "2026-08-17" in text and "2026-08-23" in text
    assert "本周由你们值周" in text
    assert "You're on oncall duty this week" in text
    assert "值班看板 / Oncall board: https://appllo.example.com/oncall" in text
    assert "⚠️" not in text


def test_07_render_partial_resolution_failure_keeps_missing_person_visible():
    """Core regression test: one resolved, one not — the unresolved person
    must still appear as plain `@localpart` text (never silently dropped),
    show up in the warning line, and be reported back in `unresolved`."""
    members = ["alice@plaud.ai", "bob@plaud.ai"]
    id_map = {"alice@plaud.ai": "ou_alice"}  # bob missing
    text, unresolved = oncall_weekly_greeting._render_greeting(
        members, id_map, date(2026, 8, 17), date(2026, 8, 23), "",
    )

    assert unresolved == ["bob@plaud.ai"]
    assert '<at user_id="ou_alice">alice</at>' in text
    assert "@bob" in text
    assert '<at user_id="ou_bob">' not in text  # bob never resolved to a mention tag
    assert "⚠️ 以下同学的飞书账号未解析出来、@ 不到，请人工同步 / could not be @-mentioned:" in text
    assert "bob@plaud.ai" in text


def test_08_render_total_resolution_failure_still_produces_text():
    members = ["alice@plaud.ai", "bob@plaud.ai"]
    text, unresolved = oncall_weekly_greeting._render_greeting(
        members, {}, date(2026, 8, 17), date(2026, 8, 23), "",
    )

    assert unresolved == members
    assert "@alice" in text
    assert "@bob" in text
    assert "<at user_id=" not in text
    assert "⚠️" in text
    assert text  # still gets rendered/sent, degraded but not blocked


def test_09_render_empty_base_url_omits_board_line_entirely():
    members = ["alice@plaud.ai"]
    id_map = {"alice@plaud.ai": "ou_alice"}
    text, _ = oncall_weekly_greeting._render_greeting(
        members, id_map, date(2026, 8, 17), date(2026, 8, 23), "",
    )

    assert "值班看板" not in text
    assert "Oncall board" not in text


def test_09b_render_base_url_trailing_slash_normalized_to_single_slash():
    """`base_url` with a trailing slash must not produce a double slash before
    `oncall` — matches the `.rstrip("/")` normalization already used in
    `app/api/oncall.py::get_my_workload`."""
    members = ["alice@plaud.ai"]
    id_map = {"alice@plaud.ai": "ou_alice"}
    text, _ = oncall_weekly_greeting._render_greeting(
        members, id_map, date(2026, 8, 17), date(2026, 8, 23), "http://host:3000/",
    )

    assert "http://host:3000/oncall" in text
    assert "http://host:3000//oncall" not in text


def test_09c_render_empty_base_url_logs_warning(caplog):
    members = ["alice@plaud.ai"]
    id_map = {"alice@plaud.ai": "ou_alice"}
    with caplog.at_level("WARNING", logger="jarvis.oncall_weekly_greeting"):
        oncall_weekly_greeting._render_greeting(
            members, id_map, date(2026, 8, 17), date(2026, 8, 23), "",
        )

    assert any("frontend_base_url resolved empty" in rec.message for rec in caplog.records)


# ---------------------------------------------------------------------------
# 10-19: send_weekly_greeting
# ---------------------------------------------------------------------------
async def test_10_happy_path_sends_marks_logs_no_admin_notification(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    await _seed_single_group(["leon@plaud.ai", "yunze@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    log_calls = []

    async def fake_log_event(event_type, **kwargs):
        log_calls.append((event_type, kwargs))

    monkeypatch.setattr(db, "log_event", fake_log_event)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))

    assert result["sent"] is True
    assert result["skipped"] is False
    assert len(send_calls) == 1
    assert send_calls[0].get("chat_id") == "oc_group_chat"
    assert "<at" in send_calls[0].get("text", "")

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == "2026-08-17"

    assert len(log_calls) == 1
    assert log_calls[0][0] == "oncall_weekly_greeting"

    admin_calls = [c for c in send_calls if c.get("email") == "admin@plaud.ai"]
    assert admin_calls == []


async def test_11_no_oncall_members_notifies_admin_and_skips(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    await db.save_oncall_groups([[]], created_by="test")  # a group exists, but it's empty
    await db.set_oncall_config("start_date", "2020-01-06")

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))

    assert result["skipped"] is True
    assert result["reason"] == "no_oncall_members"
    assert all(c.get("chat_id") != "oc_group_chat" for c in send_calls)

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == ""

    admin_calls = [c for c in send_calls if c.get("email") == "admin@plaud.ai"]
    assert len(admin_calls) == 1


async def test_12_no_start_date_notifies_admin_and_skips(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    # no save_oncall_groups, no start_date configured at all

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))

    assert result["skipped"] is True
    assert result["reason"] == "no_start_date"
    assert all(c.get("chat_id") != "oc_group_chat" for c in send_calls)

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == ""

    admin_calls = [c for c in send_calls if c.get("email") == "admin@plaud.ai"]
    assert len(admin_calls) == 1


async def test_12b_dry_run_suppresses_admin_notification_no_start_date(client, monkeypatch):
    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    # no start_date configured

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17), dry_run=True)

    assert result["skipped"] is True
    assert result["reason"] == "no_start_date"
    assert send_calls == []  # dry_run previews must never page anyone


async def test_12b_dry_run_suppresses_admin_notification_no_members(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    await db.save_oncall_groups([[]], created_by="test")
    await db.set_oncall_config("start_date", "2020-01-06")

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17), dry_run=True)

    assert result["skipped"] is True
    assert result["reason"] == "no_oncall_members"
    assert send_calls == []


async def test_13_second_call_same_week_is_a_noop(client, monkeypatch):
    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    r1 = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))
    r2 = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))

    assert r1["sent"] is True
    assert r2["skipped"] is True
    assert r2["reason"] == "already_sent_this_week"
    group_sends = [c for c in send_calls if c.get("chat_id") == "oc_group_chat"]
    assert len(group_sends) == 1


async def test_14_force_bypasses_idempotency_guard(client, monkeypatch):
    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    r1 = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17))
    r2 = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17), force=True)

    assert r1["sent"] is True
    assert r2["sent"] is True
    assert r2["skipped"] is False
    group_sends = [c for c in send_calls if c.get("chat_id") == "oc_group_chat"]
    assert len(group_sends) == 2


async def test_15_dry_run_never_sends_or_writes_marker(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return True

    async def fake_get_chat_info(chat_id):
        return {"name": "APP Team"}

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)
    monkeypatch.setattr(oncall_weekly_greeting, "get_chat_info", fake_get_chat_info)

    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 17), dry_run=True)

    assert send_calls == []
    assert result["sent"] is False
    assert result["skipped"] is False
    assert "<at" in result["text"]
    assert result.get("chat_name") == "APP Team"

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == ""


async def test_16_send_failure_retries_up_to_max_attempts_then_gives_up(client, monkeypatch, caplog):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return False

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    log_calls = []

    async def fake_log_event(event_type, **kwargs):
        log_calls.append((event_type, kwargs))

    monkeypatch.setattr(db, "log_event", fake_log_event)

    with caplog.at_level("ERROR", logger="jarvis.oncall_weekly_greeting"):
        result = await oncall_weekly_greeting.send_weekly_greeting(
            today=date(2026, 8, 17), max_attempts=3, retry_delay_s=0,
        )

    group_sends = [c for c in send_calls if c.get("chat_id") == "oc_group_chat"]
    admin_calls = [c for c in send_calls if c.get("email") == "admin@plaud.ai"]
    assert len(group_sends) == 3
    assert len(admin_calls) == 1  # admin notified about the total failure, distinct from the 3 group attempts
    assert len(send_calls) == 4
    assert result["sent"] is False
    assert result["reason"] == "send_failed"

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == ""

    assert len(log_calls) == 1
    assert log_calls[0][1]["detail"]["sent"] is False

    assert any("failed to send weekly greeting" in rec.message for rec in caplog.records)


async def test_16b_send_failure_to_email_never_notifies_admin(client, monkeypatch):
    """A failed `to_email` verification ping is the admin's own manual check —
    notifying them about their own manual check failing would be redundant, so
    the total-failure admin notification must not fire on that path."""
    _patch_settings(monkeypatch, chat_id="oc_group_chat", feedback_recipient="admin@plaud.ai")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message(**kwargs):
        send_calls.append(kwargs)
        return False

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    result = await oncall_weekly_greeting.send_weekly_greeting(
        today=date(2026, 8, 17), to_email="someone@plaud.ai", max_attempts=3, retry_delay_s=0,
    )

    assert result["sent"] is False
    admin_calls = [c for c in send_calls if c.get("email") == "admin@plaud.ai"]
    assert admin_calls == []
    assert len(send_calls) == 3  # only the 3 to_email attempts, no admin ping


async def test_17_send_succeeds_on_second_attempt(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    call_count = {"n": 0}

    async def fake_send_message(**kwargs):
        call_count["n"] += 1
        return call_count["n"] >= 2

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    result = await oncall_weekly_greeting.send_weekly_greeting(
        today=date(2026, 8, 17), retry_delay_s=0,
    )

    assert call_count["n"] == 2
    assert result["sent"] is True

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == "2026-08-17"


async def test_18_to_email_verification_ping_never_writes_marker(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    send_calls = []

    async def fake_send_message_ok(**kwargs):
        send_calls.append(kwargs)
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message_ok)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    result = await oncall_weekly_greeting.send_weekly_greeting(
        today=date(2026, 8, 17), to_email="someone@plaud.ai",
    )

    assert result["sent"] is True
    assert len(send_calls) == 1
    assert send_calls[0].get("email") == "someone@plaud.ai"
    assert not send_calls[0].get("chat_id")

    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == ""  # success didn't write the marker either — to_email never should

    # A failed to_email send also must never write the marker.
    async def fake_send_message_fail(**kwargs):
        send_calls.append(kwargs)
        return False

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message_fail)

    result2 = await oncall_weekly_greeting.send_weekly_greeting(
        today=date(2026, 8, 17), to_email="someone2@plaud.ai", retry_delay_s=0,
    )
    assert result2["sent"] is False
    marker2 = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker2 == ""


async def test_19_marker_uses_monday_of_injected_week_not_the_injected_date(client, monkeypatch):
    from app.db import database as db

    _patch_settings(monkeypatch, chat_id="oc_group_chat")
    await _seed_single_group(["leon@plaud.ai"])

    async def fake_send_message(**kwargs):
        return True

    monkeypatch.setattr(oncall_weekly_greeting, "send_message", fake_send_message)
    monkeypatch.setattr(oncall_weekly_greeting, "_emails_to_open_id_map", _fake_id_map_identity)

    # 2026-08-19 is a midweek day; Monday of that calendar week is 2026-08-17.
    result = await oncall_weekly_greeting.send_weekly_greeting(today=date(2026, 8, 19))

    assert result["sent"] is True
    marker = await db.get_oncall_config(oncall_weekly_greeting._LAST_SENT_KEY, "")
    assert marker == "2026-08-17"
