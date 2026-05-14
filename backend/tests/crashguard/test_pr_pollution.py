"""Unit tests for pr_sync._detect_pollution — Gate#14 draft 污染兜底。"""
from __future__ import annotations

from app.crashguard.services.pr_sync import _detect_pollution


def test_clean_pr_returns_empty():
    payload = {
        "files": [
            {"path": "lib/app/data/api/auth_interceptor.dart"},
            {"path": "lib/utils/helper.dart"},
        ]
    }
    assert _detect_pollution(payload) == []


def test_pubspec_yaml_caught():
    payload = {
        "files": [
            {"path": "lib/foo.dart"},
            {"path": "pubspec.yaml"},
        ]
    }
    hits = _detect_pollution(payload)
    assert "pubspec.yaml" in hits


def test_gen_dart_caught():
    payload = {
        "files": [
            {"path": "lib/utils/analytics_params.gen.dart"},
        ]
    }
    hits = _detect_pollution(payload)
    assert hits == ["lib/utils/analytics_params.gen.dart"]


def test_lock_files_caught():
    payload = {
        "files": [
            {"path": "Podfile.lock"},
            {"path": "ios/Podfile.lock"},  # 子目录里的同名
        ]
    }
    hits = _detect_pollution(payload)
    assert set(hits) == {"Podfile.lock", "ios/Podfile.lock"}


def test_empty_files_returns_empty():
    assert _detect_pollution({"files": []}) == []
    assert _detect_pollution({}) == []


def test_freezed_dart_caught():
    payload = {"files": [{"path": "lib/model/user.freezed.dart"}]}
    assert _detect_pollution(payload) == ["lib/model/user.freezed.dart"]


def test_string_path_in_list_works():
    """fallback：files 可能是纯字符串列表（旧 gh CLI）。"""
    payload = {"files": ["pubspec.yaml", "lib/foo.dart"]}
    assert _detect_pollution(payload) == ["pubspec.yaml"]
