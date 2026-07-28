"""Weekly oncall sync FROM Feishu「本周值班」表 INTO Jarvis 排班快照。"""

from datetime import date, datetime
from zoneinfo import ZoneInfo


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
