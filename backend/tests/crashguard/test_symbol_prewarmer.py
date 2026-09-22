"""符号包预热单测（2026-09-22）。

目标：让 QA 查刚发的灰度包时永远命中 cached，而不是等 1–3 分钟下载。
这是 keep_versions 10→20 真正的配套价值（预热会占名额，keep 太小会挤掉
研发正在回查的老版本）。

依赖方向：crashguard → graygate（单向）。graygate 是独立子模块，不应知道
crashguard 存在，所以由 crashguard 主动读它的 focus_version，graygate 零改动。
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_cached_version_is_skipped_not_redownloaded(monkeypatch):
    """幂等性的关键：已缓存的版本必须跳过，否则每 30 分钟重下 90MB。"""
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.201-941", "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "cached", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    downloads = []
    async def _dl(platform, app_version, profile, repo):
        downloads.append((platform, app_version))
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["skipped"] == ["ios:4.0.201-941"]
    assert res["prewarmed"] == []
    assert downloads == []


@pytest.mark.asyncio
async def test_available_version_gets_downloaded(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": None, "android": "4.0.200-938"}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "available", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    downloads = []
    async def _dl(platform, app_version, profile, repo):
        downloads.append((platform, app_version))
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == ["android:4.0.200-938"]
    assert downloads == [("android", "4.0.200-938")]


@pytest.mark.asyncio
async def test_missing_version_logged_not_alerted(monkeypatch):
    """灰度包未上传符号包是常态，告警会变噪音 —— 只记日志，不抛、不告警。"""
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.999-1", "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "missing", "eta_hint": "", "symbol_sources": [],
                "suggestions": {"reason": "无符号资产"}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    res = await pw.prewarm_focus_versions()      # 不抛异常即通过
    assert res["missing"] == ["ios:4.0.999-1"]
    assert res["prewarmed"] == []


@pytest.mark.asyncio
async def test_no_focus_version_is_noop(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": None, "android": None}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == [] and res["skipped"] == [] and res["missing"] == []


@pytest.mark.asyncio
async def test_focus_version_read_failure_degrades(monkeypatch):
    """读 graygate 失败不能让 job 抛异常（会污染 heartbeat）。"""
    from app.crashguard.services import symbol_prewarmer as pw

    async def _boom():
        raise RuntimeError("graygate 炸了")
    monkeypatch.setattr(pw, "_get_focus_versions", _boom)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == []
    assert any("focus_version" in e for e in res["errors"])


@pytest.mark.asyncio
async def test_download_respects_symbol_profile(monkeypatch):
    """按 _profile_strategy 只调该调的 getter，不要盲调四个。
    native_android 没有 Dart，调 get_dart_symbols_dir 是纯浪费。"""
    from app.crashguard.services import symbol_prewarmer as pw
    import app.crashguard.services.github_symbols as gs

    calls = []
    async def _mapping(v, repo=""):
        calls.append("mapping"); return "/tmp/m"
    async def _native(v, repo=""):
        calls.append("native"); return "/tmp/n"
    async def _dart(v, repo=""):
        calls.append("dart"); return "/tmp/d"
    async def _ios(v, repo="", asset_name=""):
        calls.append("ios"); return "/tmp/i"
    monkeypatch.setattr(gs, "get_android_mapping", _mapping)
    monkeypatch.setattr(gs, "get_android_native_symbols_dir", _native)
    monkeypatch.setattr(gs, "get_dart_symbols_dir", _dart)
    monkeypatch.setattr(gs, "get_ios_dsyms_dir", _ios)

    await pw._download_symbols(
        "android", "4.0.200-938", "native_android", "Plaud-AI/plaud-native-app"
    )
    assert "dart" not in calls          # native_android 无 Dart
    assert "mapping" in calls and "native" in calls
    assert "ios" not in calls


@pytest.mark.asyncio
async def test_download_flutter_ios_uses_flutter_asset(monkeypatch):
    """flutter_ios 用 PLAUD.dSYMs.zip；native_ios 用 Plaud-Global.dSYMs.zip。
    2026-07-14 实测确认两者资产名不同，套错会下到错误的 dSYM。"""
    from app.crashguard.services import symbol_prewarmer as pw
    import app.crashguard.services.github_symbols as gs

    seen = {}
    async def _ios(v, repo="", asset_name=""):
        seen["asset"] = asset_name
        return "/tmp/i"
    monkeypatch.setattr(gs, "get_ios_dsyms_dir", _ios)

    await pw._download_symbols("ios", "3.18.0-708", "flutter_ios", "Plaud-AI/Plaud-App")
    assert seen["asset"] == gs._ASSET_IOS_DSYM

    await pw._download_symbols("ios", "4.0.201-941", "native_ios",
                               "Plaud-AI/plaud-native-app")
    assert seen["asset"] == gs._ASSET_IOS_DSYM_NATIVE


@pytest.mark.asyncio
async def test_one_platform_failure_does_not_abort_other(monkeypatch):
    from app.crashguard.services import symbol_prewarmer as pw

    async def _focus():
        return {"ios": "4.0.201-941", "android": "4.0.200-938"}
    monkeypatch.setattr(pw, "_get_focus_versions", _focus)

    async def _pf(platform, app_version, **kw):
        return {"status": "available", "eta_hint": "", "symbol_sources": [],
                "suggestions": {}, "warnings": []}
    monkeypatch.setattr(pw, "_preflight", _pf)

    async def _dl(platform, app_version, profile, repo):
        if platform == "ios":
            raise RuntimeError("VPN 卡住")
    monkeypatch.setattr(pw, "_download_symbols", _dl)

    res = await pw.prewarm_focus_versions()
    assert res["prewarmed"] == ["android:4.0.200-938"]
    assert any("ios" in e for e in res["errors"])


def test_prewarm_config_defaults():
    from app.crashguard.config import CrashguardSettings

    s = CrashguardSettings()
    assert s.symbol_prewarm_enabled is True
    assert s.symbol_prewarm_cron == "*/30 * * * *"
