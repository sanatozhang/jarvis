"""Tests for extractor newest-era resolution (flutter vs native 4.x).

Covers the transition-period risk: a device upgraded 3.x flutter → 4.x native
keeps writing the SAME plaud.log, so one file holds both eras. The extractor
must resolve os/version/platform from the NEWEST era, not head-500 (oldest).
"""
from __future__ import annotations

from pathlib import Path

from app.services.extractor import extract_log_metadata


# Realistic log line samples (see source scan of plaud2 + plaud-native-app2).
_FLUTTER_BLOCK = """\
INFO: 2026-07-03 12:34:56.789012: [tag:startup] == [app boot]
╔╣ Timestamp: 2026-07-03T12:34:57.100000  ║ Request: POST
║  https://api.plaud.ai/v2/sync
╟ app-platform: ios
╟ app-version: 3.22.0 (722)
╟ User-Agent: PLAUD/3.22.0(build:722;iOS 17.0;Apple;iPhone15,2)
INFO: 2026-07-03 12:35:00.010000: [tag:sync] == [done]
"""

_NATIVE_IOS = """\
[2026-07-03 14:22:01.310] [INFO] [Startup] AppBuildInfo: version=4.0.100, build=813, bundleId=ai.plaud.ios.plaud, debug=false
[2026-07-03 14:22:01.320] [INFO] [Startup] DeviceInfoManager: model=iPhone16,2, os=18.5, deviceId=abc-123
[2026-07-03 14:22:01.330] [INFO] [Startup] PLogger initialized
[2026-07-03 14:22:02.000] [ERROR] [PlaudAudio] EXC_BAD_ACCESS in decoder
"""

_NATIVE_ANDROID = """\
[2026-07-03 14:22:01.310] [I] [PLAUD] DeviceInfoManager init: model=Pixel 8, brand=google, os=14
[2026-07-03 14:22:01.320] [I] [PLAUD] DatadogConfig initialized with environment: production, version: 4.0.100+813
[2026-07-03 14:22:02.000] [E] [NiceBuildApplication] NPE in bootstrap
"""


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


def test_flutter_only(tmp_path):
    p = _write(tmp_path, "plaud.log", _FLUTTER_BLOCK)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "flutter"
    assert meta["app_version"] == "3.22.0 (722)"
    assert meta["platform"] == "ios"
    assert meta["os_version"] == "iOS 17.0"
    assert meta["device_model"] == "Apple iPhone15,2"


def test_native_ios_only(tmp_path):
    p = _write(tmp_path, "plaud.log", _NATIVE_IOS)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "native"
    assert meta["app_version"] == "4.0.100"
    assert meta["platform"] == "ios"
    assert meta["os_version"] == "iOS 18.5"          # OS name prefixed → normalize_platform matches
    assert meta["device_model"] == "iPhone16,2"


def test_native_android_only(tmp_path):
    p = _write(tmp_path, "plaud.log", _NATIVE_ANDROID)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "native"
    assert meta["app_version"] == "4.0.100"
    assert meta["platform"] == "android"
    assert meta["os_version"] == "Android 14"
    assert meta["device_model"] == "Pixel 8"


def test_upgraded_device_flutter_then_native_same_file(tmp_path):
    """Flutter 3.x lines FIRST, native 4.x appended after (real upgrade path).
    Newest era wins → native, not the older flutter header at the top."""
    p = _write(tmp_path, "plaud.log", _FLUTTER_BLOCK + _NATIVE_ANDROID)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "native", "native (newest era) must win over older flutter lines"
    assert meta["app_version"] == "4.0.100"
    assert meta["platform"] == "android"
    assert meta["os_version"] == "Android 14"


def test_native_via_shared_http_headers_only(tmp_path):
    """Ground truth: real native logs ALSO emit flutter-style HTTP headers
    carrying the 4.x version (`app-version: 4.0.100 (822)`, `User-Agent:
    PLAUD/4.0.100(..;iOS 26.5;..)`). Even with no AppBuildInfo/DeviceInfoManager
    startup markers in the window, the 4.x header version must classify native."""
    native_headers_only = (
        "[2026-06-29 18:00:00.000] [INFO] [Net] request start\n"
        "│ app-platform: ios\n"
        "│ app-version: 4.0.100 (822)\n"
        "│ User-Agent: PLAUD/4.0.100(build:822;iOS 26.5;Apple;iPhone)\n"
        "[2026-06-29 18:00:01.000] [INFO] [Net] response 200\n"
    )
    p = _write(tmp_path, "plaud.log", native_headers_only)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "native"
    assert meta["platform"] == "ios"
    assert meta["os_version"] == "iOS 26.5"
    assert meta["app_version"] == "4.0.100 (822)"


def test_flutter_own_datadog_line_not_misclassified(tmp_path):
    """Flutter also uses the Datadog SDK and can log 'version: 3.x+n'. The >=4.0
    guard must keep such a log classified flutter (not native)."""
    flutter_with_dd = _FLUTTER_BLOCK + (
        "INFO: 2026-07-03 12:36:00.000000: DatadogConfig initialized with environment: production, version: 3.22.0+722\n"
    )
    p = _write(tmp_path, "plaud.log", flutter_with_dd)
    meta = extract_log_metadata([p])
    assert meta["engine"] == "flutter"
    assert meta["platform"] == "ios"
    assert meta["app_version"] == "3.22.0 (722)"
