import json

import pytest

from app.modulehub.core import result as r

ANDROID = {"coordinate": "ai.plaud.module:logger:1.0.0", "sha256": "a" * 64}
IOS = {"coordinate": "ai/plaud/module/logger-ios/1.0.0/Logger-1.0.0.xcframework.zip", "sha256": "c" * 64,
       "product": "Logger", "apiChanges": ["x"], "downloadUrls": ["u"]}
GOOD = {"schemaVersion": 2, "name": "logger", "version": "1.0.0", "gitSha": "b" * 40, "branch": "main", "dryRun": False,
        "major": False, "tag": "v1.0.0", "platforms": {"android": ANDROID, "ios": IOS}}


def test_parse_ok():
    res = r.parse_result(json.dumps(GOOD))
    assert (res.name, res.version, res.dry_run, res.tag) == ("logger", "1.0.0", False, "v1.0.0")
    assert list(res.platforms) == ["android", "ios"]
    assert res.platforms["android"].sha256 == "a" * 64 and res.platforms["android"].api_changes == []
    assert res.platforms["ios"].api_changes == ["x"] and res.platforms["ios"].download_urls == ["u"]
    assert res.platforms["ios"].as_dict() == {"coordinate": IOS["coordinate"], "sha256": "c" * 64, "apiChanges": ["x"]}


def test_one_platform_release():
    res = r.parse_result(json.dumps({**GOOD, "platforms": {"ios": IOS}}))
    assert list(res.platforms) == ["ios"]


@pytest.mark.parametrize("patch", [
    {"schemaVersion": 1}, {"version": "1.0"}, {"gitSha": "short"}, {"name": "Logger"}, {"dryRun": "no"},
    {"platforms": {}}, {"platforms": {"harmony": ANDROID}}, {"platforms": {"android": {**ANDROID, "sha256": "zz"}}},
    {"platforms": {"android": "x"}}, {"platforms": []},
])
def test_parse_rejects_invalid(patch):
    with pytest.raises(r.ResultInvalid):
        r.parse_result(json.dumps({**GOOD, **patch}))


def test_parse_rejects_missing_and_non_json():
    bad = dict(GOOD); bad.pop("platforms")
    with pytest.raises(r.ResultInvalid):
        r.parse_result(json.dumps(bad))
    with pytest.raises(r.ResultInvalid):
        r.parse_result("not json")
    with pytest.raises(r.ResultInvalid):
        r.parse_result("[1]")


def test_empty_sha_allowed_for_dry_run():
    res = r.parse_result(json.dumps({**GOOD, "dryRun": True, "platforms": {"android": {**ANDROID, "sha256": ""}}}))
    assert res.platforms["android"].sha256 == ""


def test_last_step_ignores_platform_markers():
    log = "x\nSTEP=build\nPLATFORM_STEP=ios:test\nfoo\nSTEP=upload\nPLATFORM_STEP=android:result\n"
    assert r.last_step(log) == "upload"
    assert r.last_step("no steps") is None
