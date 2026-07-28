"""Weekly oncall sync FROM Feishu「本周值班」表 INTO Jarvis 排班快照。"""

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest


def _ms(y, m, d):
    return int(datetime(y, m, d, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1000)


def test_feishu_settings_oncall_table_defaults():
    from app.config import FeishuSettings
    s = FeishuSettings()
    assert s.oncall_table_id == "tblICR3x8k7nwoNK"
    assert s.oncall_view_id == "vewpgzcUrK"


def test_emails_from_person_field_extracts_dedupes_and_lowercases():
    from app.services.oncall_feishu_sync import _emails_from_person_field
    value = [
        {"email": "Leon@Plaud.ai", "name": "Leon"},
        {"email": "", "name": "no email"},
        {"name": "missing email key"},
    ]
    assert _emails_from_person_field(value) == ["leon@plaud.ai"]


def test_emails_from_person_field_handles_none_and_empty():
    from app.services.oncall_feishu_sync import _emails_from_person_field
    assert _emails_from_person_field(None) == []
    assert _emails_from_person_field([]) == []


async def test_fetch_feishu_oncall_weeks_merges_roles_filters_old_and_empty(monkeypatch):
    from app.services import oncall_feishu_sync

    fake_response = {
        "data": {
            "items": [
                {
                    "fields": {
                        "日期": _ms(2026, 7, 27),
                        "值班人员（Feature）": [{"email": "leon@plaud.ai"}],
                        "值班人员（Fundamentals）": [{"email": "yunze@plaud.ai"}],
                    }
                },
                {
                    # 早于 min_week_start，必须被过滤掉
                    "fields": {
                        "日期": _ms(2026, 7, 13),
                        "值班人员（Feature）": [{"email": "jason.shao@plaud.ai"}],
                        "值班人员（Fundamentals）": [{"email": "victor@plaud.ai"}],
                    }
                },
                {
                    # 两个角色都是空，必须被跳过（不能把空行同步成"清空排班"）
                    "fields": {
                        "日期": _ms(2026, 8, 3),
                        "值班人员（Feature）": None,
                        "值班人员（Fundamentals）": None,
                    }
                },
            ]
        }
    }

    async def fake_run_cli(*args, **kwargs):
        return fake_response

    monkeypatch.setattr(oncall_feishu_sync, "_run_cli", fake_run_cli)

    weeks = await oncall_feishu_sync.fetch_feishu_oncall_weeks(date(2026, 7, 27))

    assert weeks == [
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]},
    ]


async def test_diff_and_sync_overwrites_changed_week(client):
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["a@plaud.ai"], ["b@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")  # 周一，week_num 0 = 2026-07-27

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]},
    ])

    assert result["skipped"] is False
    assert len(result["updated"]) == 1
    assert result["updated"][0]["before"] == ["a@plaud.ai"]
    assert result["updated"][0]["after"] == ["leon@plaud.ai", "yunze@plaud.ai"]

    snap = await db.get_week_assignment(date(2026, 7, 27))
    assert snap["members"] == ["leon@plaud.ai", "yunze@plaud.ai"]
    # groups = [["a@plaud.ai"], ["b@plaud.ai"]], start_date week_num 0 -> pre-overwrite
    # resolve_week_group 算出 group_index=0（对应 "a@plaud.ai"，即 before）。这是
    # 覆盖前那次 rotation 自己的 index，不是哨兵值——续轮锚点链不会被这次写入干扰。
    assert snap["group_index"] == 0


async def test_diff_and_sync_dry_run_previews_without_writing(client):
    """dry_run=True 必须算出跟真跑一样的 updated/unchanged 分类,但不能真的写库——
    这是手动触发端点绕过 kill switch 的安全阀,预览必须"零写入"。"""
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["a@plaud.ai"], ["b@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")  # 周一，week_num 0 = 2026-07-27

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]},
    ], dry_run=True)

    assert result["skipped"] is False
    assert len(result["updated"]) == 1
    assert result["updated"][0]["before"] == ["a@plaud.ai"]
    assert result["updated"][0]["after"] == ["leon@plaud.ai", "yunze@plaud.ai"]

    # 关键断言：dry_run 分类出了"会改"，但实际没有写任何东西进快照表。
    assert await db.get_week_assignment(date(2026, 7, 27)) is None


async def test_diff_and_sync_skips_unchanged_week(client):
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["leon@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai"]},
    ])

    assert result["updated"] == []
    assert len(result["unchanged"]) == 1
    assert await db.get_week_assignment(date(2026, 7, 27)) is None


async def test_diff_and_sync_aligns_to_non_monday_grid(client):
    """start_date 不是周一时(如 2026-02-10 是周二),写入的 key 必须是 Jarvis 自己
    网格算出来的日期,不能直接用 Feishu 的周一日期当 key——否则以后
    resolve_week_group 永远查不到这行,同步等于白做。"""
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["old@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-02-10")  # 周二

    feishu_week_start = date(2026, 7, 27)
    result = await diff_and_sync_oncall([
        {"week_start": feishu_week_start, "members": ["leon@plaud.ai"]},
    ])

    assert len(result["updated"]) == 1
    start = date(2026, 2, 10)
    week_num = (feishu_week_start - start).days // 7
    expected_key = start + timedelta(weeks=week_num)
    assert expected_key != feishu_week_start  # 前提：网格确实错位，测试才有意义

    assert await db.get_week_assignment(feishu_week_start) is None  # 没写在飞书原始日期上
    snap = await db.get_week_assignment(expected_key)
    assert snap is not None
    assert snap["members"] == ["leon@plaud.ai"]


async def test_diff_and_sync_skips_week_before_start_date(client):
    """Feishu 周早于配置的 start_date（week_num < 0）必须被跳过——不落进
    updated/unchanged，也不写任何快照。"""
    from app.db import database as db
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    await db.save_oncall_groups([["leon@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")  # 周一

    stale_week_start = date(2026, 7, 20)  # 早于 start_date 一周，week_num = -1
    result = await diff_and_sync_oncall([
        {"week_start": stale_week_start, "members": ["someone@plaud.ai"]},
    ])

    assert result["skipped"] is False
    assert result["updated"] == []
    assert result["unchanged"] == []
    assert await db.get_week_assignment(stale_week_start) is None


async def test_diff_and_sync_skipped_when_no_start_date(client):
    from app.services.oncall_feishu_sync import diff_and_sync_oncall

    result = await diff_and_sync_oncall([
        {"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai"]},
    ])
    assert result["skipped"] is True
    assert result["updated"] == []


def test_seconds_until_next_monday_8am_same_day_before_8():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    # 2026-07-27 是周一
    now = datetime(2026, 7, 27, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    assert _seconds_until_next_monday_8am(now) == 2 * 3600


def test_seconds_until_next_monday_8am_same_day_after_8_goes_to_next_week():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    now = datetime(2026, 7, 27, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    expected = (datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
    assert _seconds_until_next_monday_8am(now) == expected


def test_seconds_until_next_monday_8am_midweek():
    from app.services.oncall_feishu_sync import _seconds_until_next_monday_8am
    # 2026-07-29 是周三
    now = datetime(2026, 7, 29, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    expected = (datetime(2026, 8, 3, 8, 0, tzinfo=ZoneInfo("Asia/Shanghai")) - now).total_seconds()
    assert _seconds_until_next_monday_8am(now) == expected


async def test_sync_oncall_from_feishu_end_to_end(client, monkeypatch):
    from app.db import database as db
    from app.services import oncall_feishu_sync

    await db.save_oncall_groups([["old@plaud.ai"]], created_by="test")
    await db.set_oncall_config("start_date", "2026-07-27")

    async def fake_fetch(min_week_start):
        assert min_week_start == date(2026, 7, 27)
        return [{"week_start": date(2026, 7, 27), "members": ["leon@plaud.ai", "yunze@plaud.ai"]}]

    monkeypatch.setattr(oncall_feishu_sync, "fetch_feishu_oncall_weeks", fake_fetch)

    result = await oncall_feishu_sync.sync_oncall_from_feishu(today=date(2026, 7, 27))

    assert result["skipped"] is False
    assert len(result["updated"]) == 1
    snap = await db.get_week_assignment(date(2026, 7, 27))
    assert snap["members"] == ["leon@plaud.ai", "yunze@plaud.ai"]


async def test_sync_oncall_from_feishu_logs_failure_and_reraises(client, monkeypatch):
    """拉取/对比失败时，必须在往上抛异常之前先记一条 log_event——否则周一 08:00
    的瞬时失败在审计日志里完全不可见（loop 那边只是记 error log，1 小时后重试，
    审计表看不到这次失败发生过）。"""
    from app.db import database as db
    from app.services import oncall_feishu_sync

    logged_calls = []

    async def fake_log_event(event_type, **kwargs):
        logged_calls.append((event_type, kwargs))

    async def fake_fetch_raises(min_week_start):
        raise RuntimeError("feishu api boom")

    monkeypatch.setattr(db, "log_event", fake_log_event)
    monkeypatch.setattr(oncall_feishu_sync, "fetch_feishu_oncall_weeks", fake_fetch_raises)

    with pytest.raises(RuntimeError, match="feishu api boom"):
        await oncall_feishu_sync.sync_oncall_from_feishu(today=date(2026, 7, 27))

    assert len(logged_calls) == 1
    event_type, kwargs = logged_calls[0]
    assert event_type == "oncall_feishu_sync"
    assert kwargs["detail"]["error"] == "feishu api boom"
    assert kwargs["detail"]["dry_run"] is False
