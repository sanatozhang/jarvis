"""crashguard yaml→settings 映射的分段归属。

回归：Slack 迁移时 `notify` 段被插进了 `feishu` 段中间，早晚报的
cron / 开关 / 窗口 5 个 key 跟着掉进了 `if "notify" in cfg` 分支——
yaml 里没有 notify 段时这 5 个 key 静默不生效；有 notify 段但没
feishu 段时直接 NameError。
"""
from unittest.mock import patch

from app.crashguard import config as cg_config


def _overrides(crashguard_cfg):
    with patch.object(cg_config, "_load_yaml", return_value={"crashguard": crashguard_cfg}):
        return cg_config._yaml_overrides()


def test_feishu_schedule_keys_read_without_notify_section():
    flat = _overrides({"feishu": {
        "morning_cron": "0 9 * * *", "evening_cron": "0 18 * * *",
        "morning_enabled": False, "evening_enabled": True,
        "evening_window_hours": 6,
    }})
    assert flat["morning_cron"] == "0 9 * * *"
    assert flat["evening_cron"] == "0 18 * * *"
    assert flat["morning_enabled"] is False
    assert flat["evening_enabled"] is True
    assert flat["evening_window_hours"] == 6


def test_notify_section_without_feishu_section():
    flat = _overrides({"notify": {"provider": "slack", "slack_channel": "C0C71G26R7S"}})
    assert flat["notify_provider"] == "slack"
    assert flat["slack_channel"] == "C0C71G26R7S"
    assert "morning_cron" not in flat
