import json

import pytest

from app.modulehub.core import result as r

GOOD = {"schemaVersion": 1, "platform": "android", "name": "logger", "version": "1.0.0",
        "coordinate": "ai.plaud.module:logger:1.0.0", "sha256": "a" * 64, "gitSha": "b" * 40,
        "branch": "main", "dryRun": False}


def test_parse_ok():
    res = r.parse_result(json.dumps(GOOD))
    assert (res.name, res.version, res.platform, res.dry_run) == ("logger", "1.0.0", "android", False)
    assert res.sha256 == "a" * 64 and res.api_changes == []


def test_parse_optional_fields():
    res = r.parse_result(json.dumps({**GOOD, "apiChanges": ["x"], "downloadUrls": ["u"]}))
    assert res.api_changes == ["x"] and res.download_urls == ["u"]


@pytest.mark.parametrize("patch", [
    {"schemaVersion": 2}, {"platform": "harmony"}, {"version": "1.0"}, {"gitSha": "short"},
    {"sha256": "zz"}, {"name": "Logger"}, {"dryRun": "no"},
])
def test_parse_rejects_invalid(patch):
    with pytest.raises(r.ResultInvalid):
        r.parse_result(json.dumps({**GOOD, **patch}))


def test_parse_rejects_missing_and_non_json():
    bad = dict(GOOD); bad.pop("version")
    with pytest.raises(r.ResultInvalid):
        r.parse_result(json.dumps(bad))
    with pytest.raises(r.ResultInvalid):
        r.parse_result("not json")
    with pytest.raises(r.ResultInvalid):
        r.parse_result("[1]")


def test_empty_sha_allowed_for_dry_run():
    assert r.parse_result(json.dumps({**GOOD, "sha256": "", "dryRun": True})).sha256 == ""


def test_last_step_from_log():
    log = "x\nSTEP=build\nfoo\nSTEP=tag\nbar\n"
    assert r.last_step(log) == "tag"
    assert r.last_step("no steps") is None
