"""Confirms config.yaml's new voc.digest_* keys actually reach Settings
through _merge_yaml_into_settings — catches the classic failure mode where a
new yaml key is added but the merge whitelist tuple isn't updated, so the
value is silently ignored and the code-level default wins instead."""
from __future__ import annotations


def test_voc_digest_settings_load_from_yaml():
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.voc.digest_enabled is True
    assert settings.voc.digest_cron == "0 10 * * 1"
    assert settings.voc.digest_push_enabled is False
    assert settings.voc.digest_model == "claude-sonnet-5"
    assert settings.voc.digest_timeout_seconds == 300
    get_settings.cache_clear()
