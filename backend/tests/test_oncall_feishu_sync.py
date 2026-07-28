"""Weekly oncall sync FROM Feishu「本周值班」表 INTO Jarvis 排班快照。"""


def test_feishu_settings_oncall_table_defaults():
    from app.config import FeishuSettings
    s = FeishuSettings()
    assert s.oncall_table_id == "tblICR3x8k7nwoNK"
    assert s.oncall_view_id == "vewpgzcUrK"
